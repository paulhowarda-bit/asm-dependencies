* DISPATCH - table-driven. Read literally it depends on NOTHING,
* which is the failure mode the unresolved inventory exists for.
DISPATCH CSECT
         STM   14,12,12(13)
         LR    12,15
         USING DISPATCH,12
         L     3,0(,1)  THE CALLER'S SELECTOR
         SLL   3,3
         LA    1,PGMTAB(3)
         LINK  EPLOC=(1)  TARGET COMPUTED AT RUN TIME
*
* A decoy: the DC below says PAYCALC and is not what the field holds.
         MVC   SELECTED,0(1)
         LINK  EPLOC=SELECTED
         LM    14,12,12(13)
         BR    14
PGMTAB   DC    CL8'DSPRTN01',CL8'DSPRTN02',CL8'DSPRTN03'
SELECTED DC    CL8'PAYCALC '
         END   DISPATCH
