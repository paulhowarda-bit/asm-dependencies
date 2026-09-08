* CUSTINQ - a CICS customer inquiry, with static and dynamic SQL.
* EXEC CICS and EXEC SQL are not assembler: they are a preprocessor's
* language in the operand field, blank-separated and with no remarks.
CUSTINQ  CSECT
         DFHEIENT CODEREG=(3),DATAREG=(13),EIBREG=(11)
*
* Quotes are the evidence: a literal name vs a data area.
         EXEC  CICS RECEIVE MAP('CUSTM01') MAPSET('CUSTSET')
*
         EXEC  SQL SELECT CUSTNAME,BALANCE
         EXEC  SQL INTO :WSNAME,:WSBAL FROM PRODDB.CUSTOMER
*
         EXEC  CICS READ FILE('CUSTMAST') INTO(CUSTREC) RIDFLD(WSKEY)
         EXEC  CICS WRITEQ TS QUEUE('CUSTLOG') FROM(CUSTREC)
*
* A literal target, and one whose name a run-time value decides.
         EXEC  CICS LINK PROGRAM('CUSTVAL') COMMAREA(CUSTREC)
         EXEC  CICS ASSIGN PROGRAM(WSPGM)
         EXEC  CICS XCTL PROGRAM(WSPGM)
*
         EXEC  CICS SEND MAP('CUSTM02')
         EXEC  CICS RETURN TRANSID('CINQ')
*
WSPGM    DS    CL8
WSKEY    DS    CL6
CUSTREC  DS    CL80
         END   CUSTINQ
